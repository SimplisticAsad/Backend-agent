# ac.project.browse  (requirements: requirement.project_management; operations: operation.project.list, operation.project.read)
Feature: Browse Projects succeeds

  Scenario: Browse Projects succeeds
    Given the user is signed in as Manager or Employee
    And the record being worked on exists
    When List Projects
    And View Project
    Then The Project information is displayed
