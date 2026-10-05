# ac.ticket.create  (requirements: requirement.ticket_management; operations: operation.ticket.create)
Feature: Create Ticket succeeds

  Scenario: Create Ticket succeeds
    Given the user is signed in as Customer
    When Fill in the Ticket form
    And Create Ticket
    Then The Ticket is created and appears in the Ticket list
