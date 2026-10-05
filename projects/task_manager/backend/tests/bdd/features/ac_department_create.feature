# ac.department.create  (requirements: requirement.department_management; operations: operation.department.create)
Feature: Create Department succeeds

  Scenario: Create Department succeeds
    Given the user is signed in as Manager
    When Fill in the Department form
    And Create Department
    Then The Department is created and appears in the Department list
